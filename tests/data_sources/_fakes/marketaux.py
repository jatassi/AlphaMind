"""In-memory fake for :class:`MarketauxAPI`.

Tests configure ``articles`` (the global response) or ``responses_by_call``
(a list of per-call payloads consumed in order).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeMarketauxAPI:
    """Stateful fake implementing :class:`MarketauxAPI`."""

    articles: list[dict[str, Any]] = field(default_factory=list)
    """Default response from ``get_news`` when no override is set."""

    articles_by_call: list[list[dict[str, Any]]] | None = None
    """Successive ``get_news`` calls consume entries in order."""

    get_news_handler: Callable[..., list[dict[str, Any]]] | None = None
    """Optional override for stateful behavior (e.g. per-symbol dispatch)."""

    connectivity: bool = True
    error: Exception | None = None

    # Call-tracking
    calls: list[dict[str, Any]] = field(default_factory=list)
    _call_index: int = 0

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get_news(
        self,
        symbols: list[str] | None,
        *,
        countries: str | None = None,
        published_after: str | None = None,
        limit: int = 3,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        call = {
            "symbols": symbols,
            "countries": countries,
            "published_after": published_after,
            "limit": limit,
            "page": page,
        }
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if self.get_news_handler is not None:
            return list(self.get_news_handler(**call))
        if self.articles_by_call is not None:
            idx = min(self._call_index, len(self.articles_by_call) - 1)
            self._call_index += 1
            return list(self.articles_by_call[idx])
        return list(self.articles)
