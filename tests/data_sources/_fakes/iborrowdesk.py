"""In-memory fake for :class:`IBorrowDeskAPI`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeIBorrowDeskAPI:
    """Stateful fake implementing :class:`IBorrowDeskAPI`."""

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Map of ticker → response payload."""

    handler: Callable[[str], dict[str, Any]] | None = None
    """Override for stateful behavior (e.g. raise on specific tickers)."""

    connectivity: bool = True

    calls: list[str] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def fetch_ticker(self, ticker: str) -> dict[str, Any]:
        self.calls.append(ticker)
        if self.handler is not None:
            return self.handler(ticker)
        if ticker in self.responses:
            return self.responses[ticker]
        # Mirror production behavior: unknown ticker without explicit handler
        # returns an empty response shape.
        return {"ticker": ticker, "daily": [], "real_time": []}
