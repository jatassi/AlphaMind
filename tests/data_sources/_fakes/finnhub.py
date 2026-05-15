"""In-memory fake for :class:`FinnhubSDK`.

Mirrors the subset of ``finnhub.Client`` AlphaMind invokes.  Tests pass
the fake into collector entry points as ``_sdk=`` rather than patching
``finnhub.Client``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeFinnhubSDK:
    """Stateful fake implementing :class:`FinnhubSDK`."""

    # Default responses
    market_status_response: dict[str, Any] = field(
        default_factory=lambda: {"exchange": "US", "isOpen": True}
    )
    company_news_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    general_news_response: list[dict[str, Any]] = field(default_factory=list)
    eps_estimates_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    revenue_estimates_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    earnings_calendar_response: dict[str, Any] = field(
        default_factory=lambda: {"earningsCalendar": []}
    )
    economic_calendar_response: dict[str, Any] = field(
        default_factory=lambda: {"economicCalendar": []}
    )
    ipo_calendar_response: dict[str, Any] = field(default_factory=lambda: {"ipoCalendar": []})
    fda_calendar_response: list[dict[str, Any]] | dict[str, Any] = field(
        default_factory=lambda: {"fdaCalendar": []}
    )

    # Per-call handler overrides (for stateful behaviour)
    company_news_handler: Callable[..., list[dict[str, Any]]] | None = None
    eps_handler: Callable[..., dict[str, Any]] | None = None
    revenue_handler: Callable[..., dict[str, Any]] | None = None
    earnings_calendar_handler: Callable[..., dict[str, Any]] | None = None
    economic_calendar_handler: Callable[..., dict[str, Any]] | None = None
    ipo_calendar_handler: Callable[..., dict[str, Any]] | None = None

    # Side-effect errors
    market_status_error: Exception | None = None
    company_news_error: Exception | None = None
    general_news_error: Exception | None = None
    eps_error: Exception | None = None
    revenue_error: Exception | None = None
    earnings_calendar_error: Exception | None = None
    economic_calendar_error: Exception | None = None
    ipo_calendar_error: Exception | None = None
    fda_calendar_error: Exception | None = None

    # Call tracking
    market_status_calls: list[dict[str, Any]] = field(default_factory=list)
    company_news_calls: list[dict[str, Any]] = field(default_factory=list)
    general_news_calls: list[str] = field(default_factory=list)
    eps_calls: list[str] = field(default_factory=list)
    revenue_calls: list[str] = field(default_factory=list)
    earnings_calendar_calls: list[dict[str, Any]] = field(default_factory=list)
    economic_calendar_calls: list[dict[str, Any]] = field(default_factory=list)
    ipo_calendar_calls: list[dict[str, Any]] = field(default_factory=list)
    fda_calendar_calls: int = 0

    # FinnhubSDK methods
    def market_status(self, exchange: str) -> dict[str, Any]:
        self.market_status_calls.append({"exchange": exchange})
        if self.market_status_error is not None:
            raise self.market_status_error
        return self.market_status_response

    def company_news(self, symbol: str, _from: str, to: str) -> list[dict[str, Any]]:
        self.company_news_calls.append({"symbol": symbol, "_from": _from, "to": to})
        if self.company_news_error is not None:
            raise self.company_news_error
        if self.company_news_handler is not None:
            return list(self.company_news_handler(symbol, _from=_from, to=to))
        return list(self.company_news_by_symbol.get(symbol, []))

    def general_news(self, category: str) -> list[dict[str, Any]]:
        self.general_news_calls.append(category)
        if self.general_news_error is not None:
            raise self.general_news_error
        return list(self.general_news_response)

    def company_eps_estimates(self, symbol: str) -> dict[str, Any]:
        self.eps_calls.append(symbol)
        if self.eps_error is not None:
            raise self.eps_error
        if self.eps_handler is not None:
            return self.eps_handler(symbol)
        return self.eps_estimates_by_symbol.get(
            symbol, {"symbol": symbol, "data": [], "freq": "quarterly"}
        )

    def company_revenue_estimates(self, symbol: str) -> dict[str, Any]:
        self.revenue_calls.append(symbol)
        if self.revenue_error is not None:
            raise self.revenue_error
        if self.revenue_handler is not None:
            return self.revenue_handler(symbol)
        return self.revenue_estimates_by_symbol.get(
            symbol, {"symbol": symbol, "data": [], "freq": "quarterly"}
        )

    def earnings_calendar(
        self, _from: str, to: str, symbol: str, international: bool = False
    ) -> dict[str, Any]:
        self.earnings_calendar_calls.append(
            {"_from": _from, "to": to, "symbol": symbol, "international": international}
        )
        if self.earnings_calendar_error is not None:
            raise self.earnings_calendar_error
        if self.earnings_calendar_handler is not None:
            return self.earnings_calendar_handler(
                _from=_from, to=to, symbol=symbol, international=international
            )
        return self.earnings_calendar_response

    def calendar_economic(self, _from: str | None = None, to: str | None = None) -> dict[str, Any]:
        self.economic_calendar_calls.append({"_from": _from, "to": to})
        if self.economic_calendar_error is not None:
            raise self.economic_calendar_error
        if self.economic_calendar_handler is not None:
            return self.economic_calendar_handler(_from=_from, to=to)
        return self.economic_calendar_response

    def ipo_calendar(self, _from: str, to: str) -> dict[str, Any]:
        self.ipo_calendar_calls.append({"_from": _from, "to": to})
        if self.ipo_calendar_error is not None:
            raise self.ipo_calendar_error
        if self.ipo_calendar_handler is not None:
            return self.ipo_calendar_handler(_from=_from, to=to)
        return self.ipo_calendar_response

    def fda_calendar(self) -> list[dict[str, Any]] | dict[str, Any]:
        self.fda_calendar_calls += 1
        if self.fda_calendar_error is not None:
            raise self.fda_calendar_error
        return self.fda_calendar_response
