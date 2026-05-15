"""Protocol declaration for the Marketaux HTTP wrapper."""

from __future__ import annotations

from typing import Any, Protocol


class MarketauxAPI(Protocol):
    """Methods AlphaMind invokes on the Marketaux HTTP wrapper."""

    def verify_connectivity(self) -> bool: ...
    def get_news(
        self,
        symbols: list[str] | None,
        *,
        countries: str | None = None,
        published_after: str | None = None,
        limit: int = 3,
        page: int = 1,
    ) -> list[dict[str, Any]]: ...
